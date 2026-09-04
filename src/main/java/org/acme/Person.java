package org.acme;

import io.quarkus.hibernate.orm.panache.PanacheEntityBase;

import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.GeneratedValue;
import javax.persistence.Id;
import javax.persistence.Table;
import javax.persistence.Version;

import java.time.Instant;

@Entity
@Table(name = "PERSON")
public class Person extends PanacheEntityBase {

    @Id
    @GeneratedValue
    public Long id;

    @Version
    public long version;

    @Column(nullable = false)
    public String firstName;

    @Column(nullable = false)
    public String lastName;

    @Column(nullable = false, unique = true)
    public String email;

    public String city;

    public boolean active = true;

    public Instant createdAt = Instant.now();
}